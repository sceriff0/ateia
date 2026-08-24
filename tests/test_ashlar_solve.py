"""Tests for bin/ashlar_solve.py — ASHLAR's tile placements as a STARE transform manifest.

The load-bearing claim of the ASHLAR arm is that ``LayerAligner.positions`` can be rewritten
as the ``M0`` + mesh manifest ``tiled_stage_warp.make_warper`` already reads, so ASHLAR is
scored by the reg_qc=2 segmentation-overlap metric rather than a parallel ORB table. These
tests pin that claim end to end WITHOUT ashlar, a container or an image: the aligner objects
are replaced by arrays whose correct answer is known by construction, and the emitted
manifest is round-tripped through the real warper.
"""

from __future__ import annotations

import numpy as np
import pytest
from ashlar_solve import (
    _yx_to_xy,
    ashlar_entry,
    control_grid,
    decompose,
    tile_displacements,
    translation_affine,
)
from tiled_manifest import build_manifest, slide_entry
from tiled_stage_warp import make_warper

N_ROWS, N_COLS = 3, 4
TILE_SIZE = 400
OVERLAP = 0.25
STRIDE = int(round(TILE_SIZE * (1 - OVERLAP)))  # 300

GRID = {
    "n_rows": N_ROWS,
    "n_cols": N_COLS,
    "tile_size": TILE_SIZE,
    "overlap": OVERLAP,
    "stride": STRIDE,
    "pattern": "r{row:03}_c{col:03}.tif",
}
OUT_SHAPE = (N_ROWS * STRIDE + TILE_SIZE, N_COLS * STRIDE + TILE_SIZE)


def meta_positions():
    """``(y, x)`` nominal tile positions, row-major — ashlar's own tile_position formula."""
    return np.array([[r * STRIDE, c * STRIDE]
                     for r in range(N_ROWS) for c in range(N_COLS)], dtype=float)


def tile_centres_xy():
    """``(x, y)`` centre of every tile in the moving slide's NATIVE frame, row-major."""
    return np.array([[c * STRIDE + TILE_SIZE / 2.0, r * STRIDE + TILE_SIZE / 2.0]
                     for r in range(N_ROWS) for c in range(N_COLS)], dtype=float)


def scenario(delta_yx, ref_stitch_error_yx=None):
    """Aligner arrays for a moving cycle whose tile *t* is displaced by ``delta_yx[t]``.

    ``ref_stitch_error_yx`` perturbs the reference EdgeAligner's own placements away from
    the nominal grid, which is what happens on real tissue — the derivation must subtract
    it back out.
    """
    mov_meta = meta_positions()
    ref_meta = meta_positions()
    ref_aligner = ref_meta + (0.0 if ref_stitch_error_yx is None else ref_stitch_error_yx)
    layer = mov_meta + np.asarray(delta_yx, dtype=float) + (ref_aligner - ref_meta)
    return layer, mov_meta, ref_aligner, ref_meta, np.arange(N_ROWS * N_COLS)


def test_displacement_recovers_a_known_per_tile_offset():
    rng = np.random.default_rng(0)
    delta = rng.normal(0, 5, size=(N_ROWS * N_COLS, 2))
    d = tile_displacements(*scenario(delta))
    np.testing.assert_allclose(d, delta)


def test_displacement_subtracts_the_reference_stitching_error():
    """A reference cycle whose own stitch moved the tiles must not leak into D.

    EdgeAligner subtracts ``origin`` and applies per-tile stitch shifts, so
    ``ref_aligner.positions != ref_meta.positions`` in general. Dropping that term is the
    easy version of this derivation and it is wrong — it would report the reference's
    stitching as cross-cycle misregistration.
    """
    rng = np.random.default_rng(1)
    delta = rng.normal(0, 5, size=(N_ROWS * N_COLS, 2))
    stitch = rng.normal(0, 17, size=(N_ROWS * N_COLS, 2))
    np.testing.assert_allclose(tile_displacements(*scenario(delta, stitch)), delta)


def test_axis_order_is_swapped_exactly_once():
    assert _yx_to_xy([[1.0, 2.0]]).tolist() == [[2.0, 1.0]]
    np.testing.assert_allclose(_yx_to_xy(_yx_to_xy([[3.0, 7.0]])), [[3.0, 7.0]])


def test_decompose_splits_into_median_translation_and_residual():
    delta = np.array([[10.0, 20.0], [10.0, 20.0], [12.0, 24.0]])
    translation, residual = decompose(delta)
    np.testing.assert_allclose(translation, [20.0, 10.0])          # (x, y), median
    np.testing.assert_allclose(residual[0], [0.0, 0.0])
    np.testing.assert_allclose(residual[2], [4.0, 2.0])


def test_decompose_ignores_discarded_tiles():
    """Discarded tiles carry model predictions, not measurements, so they must not set M0."""
    delta = np.array([[10.0, 10.0]] * 5 + [[900.0, 900.0]] * 4)
    discard = np.array([False] * 5 + [True] * 4)
    translation, _ = decompose(delta, discard)
    np.testing.assert_allclose(translation, [10.0, 10.0])


def test_translation_affine_is_the_manifest_forward_convention():
    """M0 must map native moving (x, y) into the reference frame, as tiled_stage_warp applies it."""
    m = np.asarray(translation_affine([3.0, -5.0]))
    xy = np.array([[100.0, 200.0]])
    homog = np.column_stack([xy, np.ones(len(xy))])
    np.testing.assert_allclose((homog @ m.T)[:, :2], [[103.0, 195.0]])


def test_control_grid_nodes_are_ascending_and_live_in_the_reference_frame():
    grid_x, grid_y = control_grid(N_ROWS, N_COLS, TILE_SIZE, STRIDE, [7.0, -3.0])
    assert len(grid_x) == N_COLS and len(grid_y) == N_ROWS
    assert grid_x == sorted(grid_x) and grid_y == sorted(grid_y)   # MeshField requires ascending
    assert grid_x[0] == 0 * STRIDE + TILE_SIZE / 2 + 7.0
    assert grid_y[1] == 1 * STRIDE + TILE_SIZE / 2 - 3.0


# --- the round trip: manifest -> the real reg_qc=2 warper -------------------------------

def warper_for(delta_yx, discard=None):
    d = tile_displacements(*scenario(delta_yx))
    entry, translation = ashlar_entry(d, discard, GRID, OUT_SHAPE)
    manifest = build_manifest("ref", {"ref": slide_entry(np.eye(3)), "mov": entry})
    return make_warper(manifest), translation, entry


def test_manifest_warps_tile_centres_by_exactly_the_ashlar_displacement():
    """The whole claim, in one assertion.

    At a control node the mesh interpolation is exact, so warping a tile centre through the
    ``refined`` stage must reproduce that tile's ASHLAR displacement to floating point.
    """
    rng = np.random.default_rng(2)
    delta_yx = rng.normal(0, 9, size=(N_ROWS * N_COLS, 2))
    warp, _, _ = warper_for(delta_yx)

    centres = tile_centres_xy()
    got = warp("mov", centres, "refined")
    # Swapped literally here, not via _yx_to_xy: an expectation built from the helper under
    # test cancels a broken swap instead of catching it.
    expected = centres + np.stack([delta_yx[:, 1], delta_yx[:, 0]], axis=1)
    np.testing.assert_allclose(got, expected, atol=1e-9)


def test_rigid_stage_is_the_global_translation_only():
    rng = np.random.default_rng(3)
    delta_yx = rng.normal(0, 9, size=(N_ROWS * N_COLS, 2))
    warp, translation, _ = warper_for(delta_yx)
    centres = tile_centres_xy()
    np.testing.assert_allclose(warp("mov", centres, "rigid"), centres + translation, atol=1e-9)


def test_native_stage_is_the_identity():
    warp, _, _ = warper_for(np.full((N_ROWS * N_COLS, 2), 4.0))
    centres = tile_centres_xy()
    np.testing.assert_allclose(warp("mov", centres, "native"), centres)


def test_a_pure_translation_collapses_to_a_rigid_only_entry():
    """No mesh when every tile agrees — slide_entry drops an all-zero grid to mesh: None.

    This is what a cycle that only shifted should produce, and it keeps the ``refined``
    stage from claiming a non-rigid correction that is really numerical noise.
    """
    _, _, entry = warper_for(np.full((N_ROWS * N_COLS, 2), [6.0, -2.0]))
    assert entry["mesh"] is None
    np.testing.assert_allclose(entry["M0"], translation_affine([-2.0, 6.0]))


def test_mesh_displacements_are_row_major_ny_nx_2():
    """MeshField validates this shape; a transposed grid would silently mis-place every node."""
    rng = np.random.default_rng(4)
    _, _, entry = warper_for(rng.normal(0, 9, size=(N_ROWS * N_COLS, 2)))
    assert np.asarray(entry["mesh"]["displacements"]).shape == (N_ROWS, N_COLS, 2)
    assert entry["out_shape"] == [OUT_SHAPE[0], OUT_SHAPE[1]]


def test_grid_mismatch_is_refused_rather_than_reshaped():
    with pytest.raises(ValueError, match="row-major"):
        ashlar_entry(np.zeros((N_ROWS * N_COLS - 1, 2)), None, GRID, OUT_SHAPE)
