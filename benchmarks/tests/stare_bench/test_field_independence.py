"""The circularity firewall, as an executable assertion.

If the ground-truth field were representable by a bilinear control grid at
reg_tiled_tile spacing, STARE would be scored on its own model class and the
benchmark would be rigged. This test fits exactly that grid and asserts the fit
FAILS to reproduce the field.

The companion test builds a field deliberately ON such a grid and asserts the
guard catches it -- without that, a guard that always passes looks identical to
a guard that works.
"""
import re
from pathlib import Path

import numpy as np
import pytest
from scipy.interpolate import RegularGridInterpolator

from benchmarks.stare_bench import fields as fields_mod
from benchmarks.stare_bench.fields import Field, make_field

REG_TILED_TILE = 2048  # nextflow.config, reg_tiled_mode=high
SHAPE = (4096, 4096)
# Below this, the control grid has reproduced the field and the firewall is gone.
MIN_RESIDUAL_FRACTION = 0.25

REPO_ROOT = Path(__file__).resolve().parents[3]
REG_PRESETS_GROOVY = REPO_ROOT / "lib" / "RegPresets.groovy"
_HIGH_ROW_RE = re.compile(r"high\s*:\s*\[([^\]]*)\]")
_TILE_KEY_RE = re.compile(r"tile\s*:\s*(\d+)")


def _high_tile_from_reg_presets():
    """Parse the ACTUAL `tile` value out of `RegPresets.STARE`'s `high` row.

    `lib/RegPresets.groovy` is the one real source of STARE's control-grid
    spacing at `reg_tiled_mode=high` (nextflow.config's shipped default).
    `fields.py`, `plan.py` and this module each hardcode a copy
    (`REG_TILED_TILE = 2048`) that is correct today but tied to nothing: a
    mid-rung run at a different `reg_tiled_mode` (e.g. `medium`, tile 1024)
    would leave the firewall's `MAX_COARSE_PITCH_PX` exactly twice as lax as
    it needs to be, and nothing would notice. This reads the same discipline
    `tests/test_resource_label_coverage.py` uses for its resource numbers:
    parse the doc/source that is supposed to be authoritative, don't trust
    the copy from memory.
    """
    text = REG_PRESETS_GROOVY.read_text()
    high_row = _HIGH_ROW_RE.search(text)
    assert high_row, (
        f"could not find RegPresets.STARE's 'high' row in {REG_PRESETS_GROOVY}"
    )
    tile = _TILE_KEY_RE.search(high_row.group(1))
    assert tile, f"could not find a 'tile' key in the 'high' row: {high_row.group(1)!r}"
    return int(tile.group(1))


def _fit_control_grid_residual(field, shape, tile):
    """Fit STARE's model -- a bilinear control grid at tile centres -- and
    return residual_rms / field_rms over a dense check set."""
    h, w = shape
    gxs = np.arange(tile / 2, w, tile, dtype=float)
    gys = np.arange(tile / 2, h, tile, dtype=float)
    gx, gy = np.meshgrid(gxs, gys)
    nodes = np.stack([gx.ravel(), gy.ravel()], axis=1)
    node_disp = field.sample(nodes).reshape(gys.size, gxs.size, 2)

    rng = np.random.default_rng(0)
    check = np.stack([rng.uniform(0, w - 1, 4000), rng.uniform(0, h - 1, 4000)], axis=1)
    truth = field.sample(check)

    fitted = np.empty_like(truth)
    for k in range(2):
        fitted[:, k] = _bilinear(node_disp[:, :, k], gxs, gys, check)

    field_rms = np.sqrt((truth**2).sum(axis=1).mean())
    resid_rms = np.sqrt(((truth - fitted) ** 2).sum(axis=1).mean())
    return resid_rms / field_rms


def _bilinear(grid, gxs, gys, xy):
    """Bilinear lookup of ``grid`` at ``xy``, via scipy -- deliberately NOT
    ``fields.py``'s hand-rolled ``_bilinear_on_grid``. If this fitter shared
    that implementation, a latent bug in its boundary-clamping logic would
    corrupt the ground-truth field and the fit in the same direction and this
    guard could never see it.

    Query coordinates are clamped to the node hull ``[gxs[0], gxs[-1]] x
    [gys[0], gys[-1]]`` BEFORE interpolation. This mirrors
    ``bin/utils/mesh_field.py``'s ``_bilinear_weights``, which edge-clamps so
    STARE's real mesh extrapolates as a constant past its outermost control
    points. ``RegularGridInterpolator`` does not do this on its own --
    unclamped, it either extrapolates linearly (``fill_value=None``) or
    substitutes a constant everywhere outside the hull (a numeric
    ``fill_value``), either of which models a different transform from the
    one STARE actually applies and would silently change what
    ``MIN_RESIDUAL_FRACTION`` means. Do not "simplify" the clamp away.
    """
    x = np.clip(xy[:, 0], gxs[0], gxs[-1])
    y = np.clip(xy[:, 1], gys[0], gys[-1])
    interp = RegularGridInterpolator((gys, gxs), grid, method="linear")
    return interp(np.stack([y, x], axis=1))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_headline_field_is_not_representable_by_a_control_grid(seed):
    field = make_field("random_fourier", SHAPE, seed=seed,
                       correlation_px=777.0, amplitude_px=12.0)
    frac = _fit_control_grid_residual(field, SHAPE, REG_TILED_TILE)
    assert frac > MIN_RESIDUAL_FRACTION, (
        f"a bilinear control grid at {REG_TILED_TILE}px reproduced the field to "
        f"within {frac:.3f} of its own magnitude. The firewall is gone: STARE "
        f"would be scored on its own model class."
    )


def test_the_guard_catches_a_field_built_on_the_control_grid():
    """A field built ON the grid must FAIL the guard, or the guard proves nothing."""
    rng = np.random.default_rng(0)
    h, w = SHAPE
    gxs = np.arange(REG_TILED_TILE / 2, w, REG_TILED_TILE, dtype=float)
    gys = np.arange(REG_TILED_TILE / 2, h, REG_TILED_TILE, dtype=float)
    nodes = rng.normal(scale=12.0, size=(gys.size, gxs.size, 2))

    def sampler(xy):
        return np.stack([_bilinear(nodes[:, :, k], gxs, gys, xy) for k in range(2)],
                        axis=1)

    cheating = Field(SHAPE, {"family": "control_grid"}, sampler)
    frac = _fit_control_grid_residual(cheating, SHAPE, REG_TILED_TILE)
    assert frac < MIN_RESIDUAL_FRACTION, (
        "the guard failed to detect a field built exactly on STARE's control "
        "grid, so it is not checking what it claims to check"
    )


def test_correlation_length_is_never_a_multiple_of_the_tile():
    for corr in (777.0, 333.0, 1111.0):
        assert corr % REG_TILED_TILE != 0
        assert REG_TILED_TILE % corr != 0


def test_reg_tiled_tile_matches_the_high_preset_in_reg_presets_groovy():
    """Pin the hardcoded copy to the real source.

    `fields.REG_TILED_TILE` (and this module's own `REG_TILED_TILE`) are
    correct today only because `reg_tiled_mode=high`'s tile happens to still
    be 2048 in `lib/RegPresets.groovy`. Nothing ties them together, so a
    preset change would silently desynchronise the firewall from what the
    pipeline actually runs. (`plan.py` no longer keeps its own copy: its
    validation now calls `fields.make_field` directly -- see
    `plan._validate_fields_are_independent`.) See this test's own module
    docstring / `_high_tile_from_reg_presets` for why that matters.
    """
    high_tile = _high_tile_from_reg_presets()
    assert fields_mod.REG_TILED_TILE == high_tile
    assert REG_TILED_TILE == high_tile


def test_the_guard_catches_it_on_a_non_square_shape_too():
    """A square SHAPE gives a 2x2 node grid, so an x/y axis swap in
    ``_fit_control_grid_residual``'s ``.reshape(gys.size, gxs.size, 2)`` would
    be invisible there. Re-run the cheating-grid construction on a
    non-square raster, where a transposed axis would misalign nodes with
    displacements and stop reproducing the field.
    """
    shape = (4096, 6144)
    rng = np.random.default_rng(0)
    h, w = shape
    gxs = np.arange(REG_TILED_TILE / 2, w, REG_TILED_TILE, dtype=float)
    gys = np.arange(REG_TILED_TILE / 2, h, REG_TILED_TILE, dtype=float)
    nodes = rng.normal(scale=12.0, size=(gys.size, gxs.size, 2))

    def sampler(xy):
        return np.stack([_bilinear(nodes[:, :, k], gxs, gys, xy) for k in range(2)],
                        axis=1)

    cheating = Field(shape, {"family": "control_grid"}, sampler)
    frac = _fit_control_grid_residual(cheating, shape, REG_TILED_TILE)
    assert frac < MIN_RESIDUAL_FRACTION, (
        "the guard failed to detect a field built exactly on STARE's control "
        "grid on a non-square raster -- an axis swap could be hiding here"
    )
