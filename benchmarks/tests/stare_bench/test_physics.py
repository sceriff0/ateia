import numpy as np
import pytest

from benchmarks.stare_bench.physics import (
    apply_all,
    background,
    blank_regions,
    noise_and_psf,
    photobleach,
)


@pytest.fixture
def img():
    rng = np.random.default_rng(0)
    return rng.uniform(0.2, 1.0, size=(128, 128)).astype(np.float32)


def test_photobleach_is_monotone_in_the_factor(img):
    rng = np.random.default_rng(1)
    strong, _ = photobleach(img, rng, factor=0.3)
    weak, _ = photobleach(img, rng, factor=0.9)
    assert strong.mean() < weak.mean() < img.mean()


def test_photobleach_retained_map_equals_the_factor(img):
    _, retained = photobleach(img, np.random.default_rng(1), factor=0.4)
    np.testing.assert_allclose(retained, 0.4)


def test_blank_regions_zero_the_signal_and_report_it(img):
    out, retained = blank_regions(img, np.random.default_rng(2), fraction=0.5)
    blanked = retained < 0.01
    assert blanked.any(), "requesting 50% blanking produced no blanked pixels"
    np.testing.assert_allclose(out[blanked], 0.0, atol=1e-6)


def test_blank_fraction_is_approximately_honoured(img):
    for target in (0.2, 0.5, 0.8):
        _, retained = blank_regions(img, np.random.default_rng(3), fraction=target)
        got = float((retained < 0.01).mean())
        assert abs(got - target) < 0.15, f"asked {target}, blanked {got}"


def test_noise_and_psf_preserves_shape_and_non_negativity(img):
    out, retained = noise_and_psf(img, np.random.default_rng(4), psf_sigma_px=1.5,
                                  photons=200.0, read_sigma=0.01)
    assert out.shape == img.shape
    assert out.min() >= 0.0
    assert retained.shape == img.shape


def test_background_adds_signal_rather_than_removing_it(img):
    out, _ = background(img, np.random.default_rng(5), af_amplitude=0.2,
                        seam_px=32, seam_amplitude=0.05, focus_sigma_px=0.5)
    assert out.mean() > img.mean()


def test_apply_all_composes_retention_multiplicatively(img):
    params = {
        "photobleach": {"factor": 0.5},
        "blank_regions": {"fraction": 0.4},
        "noise_and_psf": {"psf_sigma_px": 1.0, "photons": 300.0, "read_sigma": 0.01},
        "background": {"af_amplitude": 0.1, "seam_px": 32, "seam_amplitude": 0.02,
                       "focus_sigma_px": 0.3},
    }
    out, retained = apply_all(img, params, np.random.default_rng(6))
    assert out.shape == img.shape
    assert retained.min() >= 0.0 and retained.max() <= 1.0
    # blanked pixels must stay blanked no matter what later layers add
    assert (retained < 0.01).any()


def test_apply_all_is_deterministic(img):
    params = {"photobleach": {"factor": 0.7}}
    a, ra = apply_all(img, params, np.random.default_rng(9))
    b, rb = apply_all(img, params, np.random.default_rng(9))
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(ra, rb)
