"""Full-reference (ground-truth) metric tests — the validation-mode metrics."""
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.metrics_reference import ssim, rmse, psnr, flatfield_error
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_ssim_identical_is_one():
    d = make_synthetic_mosaic(n_channels=1)
    x = d["mosaic"][0].astype(np.float64)
    assert ssim(x, x) > 0.999


def test_ssim_collapses_for_zeroed_image():
    d = make_synthetic_mosaic(vignette_strength=0.5, n_channels=1)
    x = d["mosaic"][0].astype(np.float64)
    # a black image is the over-subtraction degenerate optimum: SSIM must punish it
    assert ssim(x, np.zeros_like(x)) < 0.2
    assert ssim(x, np.zeros_like(x)) < ssim(x, x)


def test_rmse_and_psnr_directions():
    d = make_synthetic_mosaic(vignette_strength=0.5, n_channels=1)
    x = d["mosaic"][0].astype(np.float64)
    noisy = x + 50.0
    assert rmse(x, x) == 0.0
    assert rmse(x, noisy) > 0.0
    assert psnr(x, x) == float("inf")
    assert psnr(x, noisy) < psnr(x, x + 1.0)   # more error -> lower PSNR


def test_flatfield_error_zero_for_same_shape_field():
    d = make_synthetic_mosaic(vignette_strength=0.5, n_channels=1)
    ff = d["flatfield"]
    assert flatfield_error(ff, ff) < 1e-9
    assert flatfield_error(ff * 3.0, ff) < 1e-6   # scale-invariant (mean-1 normalized)
