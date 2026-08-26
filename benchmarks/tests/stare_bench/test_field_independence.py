"""The circularity firewall, as an executable assertion.

If the ground-truth field were representable by a bilinear control grid at
reg_tiled_tile spacing, STARE would be scored on its own model class and the
benchmark would be rigged. This test fits exactly that grid and asserts the fit
FAILS to reproduce the field.

The companion test builds a field deliberately ON such a grid and asserts the
guard catches it -- without that, a guard that always passes looks identical to
a guard that works.
"""
import numpy as np
import pytest

from benchmarks.stare_bench.fields import Field, make_field

REG_TILED_TILE = 2048  # nextflow.config, reg_tiled_mode=high
SHAPE = (4096, 4096)
# Below this, the control grid has reproduced the field and the firewall is gone.
MIN_RESIDUAL_FRACTION = 0.25


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
    x = np.clip(xy[:, 0], gxs[0], gxs[-1])
    y = np.clip(xy[:, 1], gys[0], gys[-1])
    ix1 = np.clip(np.searchsorted(gxs, x, side="right"), 1, gxs.size - 1)
    iy1 = np.clip(np.searchsorted(gys, y, side="right"), 1, gys.size - 1)
    ix0, iy0 = ix1 - 1, iy1 - 1
    tx = (x - gxs[ix0]) / (gxs[ix1] - gxs[ix0])
    ty = (y - gys[iy0]) / (gys[iy1] - gys[iy0])
    top = grid[iy0, ix0] * (1 - tx) + grid[iy0, ix1] * tx
    bot = grid[iy1, ix0] * (1 - tx) + grid[iy1, ix1] * tx
    return top * (1 - ty) + bot * ty


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
