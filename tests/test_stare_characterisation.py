"""Characterisation of ``tiled_pipeline.register_slide`` — the STARE in-process oracle.

This file exists to make any *unification* of the STARE algorithm provably
behaviour-preserving. ``register_slide`` is the tested-but-not-shipped copy of the method
(the shipped copy is the four ``bin/tiled_*.py`` fan-out CLIs), so before either copy is
refactored we pin exactly what ``register_slide`` computes today.

The pin is NOT a table of golden floats: ORB/RANSAC land on platform-dependent bits, and a
golden table computed on one machine is a CI failure on another. Instead the expected values
are recomputed here by an **independent, deliberately verbose re-statement of the five steps**
— the same idiom ``tests/test_tiled_reg_tile_lazy.py::_old_style_oracle`` already uses. The
oracle is written out longhand from the pre-refactor implementation; if a refactor changes
what ``register_slide`` composes (a different pre-warp, a different gate, a lost re-measure,
a decimated coarse estimate where there was none), the two diverge and this file fails.

What is pinned:
  1. The exact global anchor: ``estimate_rigid`` on the *undecimated, float64* planes.
  2. The rigid-stage residual: a WHOLE-SLIDE rigid pre-warp, then tile slices out of it.
  3. The TRE gate: ``tiled_manifest.assemble_control_grid`` (not a local re-implementation).
  4. The post-refinement re-measure, and its skip when no mesh survived the gate.
  5. The final warp, and the returned dict's key set.
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

from coarse_align import estimate_rigid  # noqa: E402
from mesh_field import MeshField  # noqa: E402
from tile_grid import tile_grid  # noqa: E402
from tile_residual import residual_displacement  # noqa: E402
from tiled_manifest import assemble_control_grid, slide_entry  # noqa: E402
from tiled_pipeline import register_slide  # noqa: E402
from tiled_warp import warp_image  # noqa: E402

RESULT_KEYS = {
    "registered",
    "entry",
    "mesh",
    "M0",
    "tiles",
    "residuals",
    "tre_px",
    "tre_after_px",
    "coarse_tre",
    "n_inliers",
}


def _textured(seed=0, n=256):
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(seed)
    img = gaussian_filter(rng.uniform(0, 1, size=(n, n)), 2.0)
    return ((img - img.min()) / (img.ptp() + 1e-9)).astype(np.float32)


def _locally_deformed(ref, amp=3.0, period=90.0):
    """A smooth non-rigid warp M0 cannot capture — guarantees the mesh branch is taken."""
    from skimage.transform import warp as sk_warp

    n = ref.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    map_x = xx + amp * np.sin(2 * np.pi * yy / period)
    map_y = yy + amp * np.cos(2 * np.pi * xx / period)
    return sk_warp(ref, np.array([map_y, map_x]), order=1, mode="reflect").astype(
        np.float32
    )


def _oracle(
    ref_dapi, mov_dapi, *, tile, halo, gate_tre, upsample=10, model="euclidean"
):
    """The pre-refactor ``register_slide``, restated longhand. Nothing is imported from it."""
    ref_dapi = np.asarray(ref_dapi, dtype=float)
    mov_dapi = np.asarray(mov_dapi, dtype=float)

    # 1. global rigid anchor, on the FULL-RESOLUTION planes (no decimation, no rescale)
    m0, coarse_tre, n_inliers = estimate_rigid(ref_dapi, mov_dapi, model=model)

    # 2. WHOLE-SLIDE rigid pre-warp, then slice each tile's read box out of it
    mov_rigid = warp_image(mov_dapi, m0, None, ref_dapi.shape)
    tiles = tile_grid(ref_dapi.shape[1], ref_dapi.shape[0], tile, halo)
    residuals = []
    for t in tiles:
        rx0, ry0, rx1, ry1 = t.read
        residuals.append(
            residual_displacement(
                ref_dapi[ry0:ry1, rx0:rx1],
                mov_rigid[ry0:ry1, rx0:rx1],
                upsample=upsample,
            )
        )

    # 3. shared TRE gate
    grid_x, grid_y, disp = assemble_control_grid(tiles, residuals, gate_tre)
    entry = slide_entry(m0, grid_x, grid_y, disp)
    mesh = (
        MeshField(grid_x, grid_y, np.asarray(disp, dtype=float))
        if entry["mesh"] is not None
        else None
    )

    # 4. post-refinement re-measure — skipped entirely when the gate left no mesh
    if mesh is not None:
        mov_refined = warp_image(mov_dapi, m0, mesh, ref_dapi.shape)
        tre_after = [
            residual_displacement(
                ref_dapi[t.read[1] : t.read[3], t.read[0] : t.read[2]],
                mov_refined[t.read[1] : t.read[3], t.read[0] : t.read[2]],
                upsample=upsample,
            )[2]
            for t in tiles
        ]
    else:
        tre_after = [r[2] for r in residuals]

    # 5. final warp
    registered = warp_image(mov_dapi, m0, mesh, ref_dapi.shape)
    return {
        "M0": m0,
        "coarse_tre": coarse_tre,
        "n_inliers": n_inliers,
        "tiles": tiles,
        "residuals": residuals,
        "entry": entry,
        "tre_after": tre_after,
        "registered": registered,
    }


def _assert_matches_oracle(result, expected):
    assert set(result.keys()) == RESULT_KEYS

    # 1. the anchor, bit-for-bit
    np.testing.assert_allclose(
        np.asarray(result["M0"], dtype=float),
        expected["M0"],
        rtol=0,
        atol=0,
        err_msg="M0 diverged from the undecimated full-resolution estimate",
    )
    assert result["coarse_tre"] == pytest.approx(expected["coarse_tre"], abs=0.0)
    assert result["n_inliers"] == expected["n_inliers"]

    # 2. tile plan + rigid-stage residuals
    assert result["tiles"] == expected["tiles"]
    assert len(result["residuals"]) == len(expected["residuals"])
    for got, want in zip(result["residuals"], expected["residuals"]):
        assert got == pytest.approx(want, abs=0.0)
    assert result["tre_px"] == pytest.approx(
        [r[2] for r in expected["residuals"]], abs=0.0
    )

    # 3. the gated manifest entry (M0 + mesh grid + displacements), as serialized JSON would be
    assert result["entry"] == expected["entry"]

    # 4. the post-refinement re-measure
    assert result["tre_after_px"] == pytest.approx(expected["tre_after"], abs=0.0)

    # 5. the warped output
    np.testing.assert_allclose(
        result["registered"], expected["registered"], rtol=0, atol=0
    )


def test_register_slide_matches_the_longhand_oracle_when_a_mesh_survives_the_gate():
    """The mesh branch: gate low enough that tiles refine, so step 4 re-measures."""
    ref = _textured(seed=2, n=256)
    mov = _locally_deformed(ref)
    kw = dict(tile=64, halo=32, gate_tre=0.5)

    result = register_slide(ref, mov, **kw)
    expected = _oracle(ref, mov, **kw)

    assert result["entry"]["mesh"] is not None, (
        "fixture no longer exercises the mesh branch"
    )
    _assert_matches_oracle(result, expected)


def test_register_slide_matches_the_longhand_oracle_when_the_gate_leaves_no_mesh():
    """The rigid-only branch: every tile gated out, so step 4 reuses the rigid residuals."""
    from scipy.ndimage import shift as ndi_shift

    ref = _textured(seed=1, n=256)
    mov = ndi_shift(ref, (4.0, -6.0), order=1, mode="reflect").astype(np.float32)
    kw = dict(tile=128, halo=32, gate_tre=0.5)

    result = register_slide(ref, mov, **kw)
    expected = _oracle(ref, mov, **kw)

    assert result["entry"]["mesh"] is None, (
        "fixture no longer exercises the rigid-only branch"
    )
    assert result["mesh"] is None
    assert result["tre_after_px"] == pytest.approx(result["tre_px"], abs=0.0)
    _assert_matches_oracle(result, expected)


def test_register_slide_warps_warp_data_not_the_dapi_when_asked():
    """``warp_data``/``out_shape`` are part of the pinned surface: the transform is estimated
    from DAPI but applied to whatever image the caller hands over."""
    ref = _textured(seed=4, n=192)
    mov = _locally_deformed(ref, amp=2.0, period=70.0)
    other = _textured(seed=5, n=192)

    result = register_slide(ref, mov, tile=64, halo=16, gate_tre=0.5, warp_data=other)
    expected = _oracle(ref, mov, tile=64, halo=16, gate_tre=0.5)

    # the estimate is unchanged by warp_data...
    np.testing.assert_allclose(
        np.asarray(result["M0"], dtype=float), expected["M0"], rtol=0, atol=0
    )
    # ...and the OUTPUT is `other` pushed through that estimate, not the DAPI.
    mesh = (
        MeshField(
            expected["entry"]["mesh"]["grid_x"],
            expected["entry"]["mesh"]["grid_y"],
            np.asarray(expected["entry"]["mesh"]["displacements"], dtype=float),
        )
        if expected["entry"]["mesh"] is not None
        else None
    )
    np.testing.assert_allclose(
        result["registered"],
        warp_image(np.asarray(other, dtype=float), expected["M0"], mesh, ref.shape),
        rtol=0,
        atol=0,
    )
