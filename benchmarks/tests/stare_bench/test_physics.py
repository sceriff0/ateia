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
    ones = np.ones_like(img)
    strong, _ = photobleach(img, ones, rng, factor=0.3)
    weak, _ = photobleach(img, ones, rng, factor=0.9)
    assert strong.mean() < weak.mean() < img.mean()


def test_photobleach_retained_map_equals_the_factor(img):
    ones = np.ones_like(img)
    _, retained = photobleach(img, ones, np.random.default_rng(1), factor=0.4)
    np.testing.assert_allclose(retained, 0.4)


def test_blank_regions_zero_the_signal_and_report_it(img):
    ones = np.ones_like(img)
    out, retained = blank_regions(img, ones, np.random.default_rng(2), fraction=0.5)
    blanked = retained < 0.01
    assert blanked.any(), "requesting 50% blanking produced no blanked pixels"
    np.testing.assert_allclose(out[blanked], 0.0, atol=1e-6)


def test_blank_fraction_is_approximately_honoured(img):
    ones = np.ones_like(img)
    for target in (0.2, 0.5, 0.8):
        _, retained = blank_regions(img, ones, np.random.default_rng(3), fraction=target)
        got = float((retained < 0.01).mean())
        # The internal convergence criterion is 0.02, so this must not be
        # tightened below that -- it would fail on the generator's own
        # rounding rather than on a real regression.
        assert abs(got - target) < 0.03, f"asked {target}, blanked {got}"


def test_noise_and_psf_preserves_shape_and_non_negativity(img):
    ones = np.ones_like(img)
    out, retained = noise_and_psf(img, ones, np.random.default_rng(4), psf_sigma_px=1.5,
                                  photons=200.0, read_sigma=0.01)
    assert out.shape == img.shape
    assert out.min() >= 0.0
    assert retained.shape == img.shape


def test_background_adds_signal_rather_than_removing_it(img):
    ones = np.ones_like(img)
    out, _ = background(img, ones, np.random.default_rng(5), af_amplitude=0.2,
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


def test_blank_regions_raises_on_non_convergence():
    """A degenerate (128, 1) shape at fraction=0.95, seed=12 misses the 0.02
    convergence window on every one of the 60 iterations (final achieved
    fraction is 1.0, error 0.05). Previously this silently returned a mask
    that did not honour the requested fraction -- since this map is consumed
    verbatim as ground truth, that would mislabel a tile's true blank
    fraction with no diagnostic at all. It must now raise instead.
    """
    degenerate = np.ones((128, 1), dtype=np.float32)
    ones = np.ones_like(degenerate)
    with pytest.raises(ValueError, match="did not converge"):
        blank_regions(degenerate, ones, np.random.default_rng(12), fraction=0.95)


def test_blank_boundary_leak_is_bounded(img):
    """Pins a real defect: the PSF in noise_and_psf mixes genuine signal in
    from neighbouring non-blanked pixels across a blank_regions boundary, so
    a pixel just outside a blank blob is more registrable than pure
    per-layer multiplication would claim. Before the fix, noise_and_psf left
    the composed retained map untouched by the blur (an all-ones per-layer
    map), so `retained < 0.01` was a lie near every blank boundary: with
    blank_regions(fraction=0.5) then noise_and_psf(psf_sigma_px=1.5) -- the
    exact sigma this module's own noise_and_psf test uses -- the buggy code
    let "blanked" pixels (retained < 0.01) carry image intensity up to 0.416
    against a 0.2-1.0 source range, with 13.6% of them above 0.05.

    After the fix, noise_and_psf blurs the retention map with the same
    sigma as the image, so a pixel that recovered visible signal from a
    neighbour also reports recovered retention and no longer reads
    `retained < 0.01`. Only pixels deep inside a blanked blob -- far enough
    from any boundary that the PSF cannot reach real signal -- still read
    `retained < 0.01`, and those carry negligible intensity. 0.05 is chosen
    with margin over what the corrected code actually produces there.
    """
    params = {
        "blank_regions": {"fraction": 0.5},
        "noise_and_psf": {"psf_sigma_px": 1.5, "photons": 200.0, "read_sigma": 0.01},
    }
    out, retained = apply_all(img, params, np.random.default_rng(7))
    deeply_blanked = retained < 0.01
    assert deeply_blanked.any(), "50% blanking produced no deeply-blanked pixels"
    assert out[deeply_blanked].max() < 0.05, (
        f"leak of {out[deeply_blanked].max():.3f} into a pixel labelled "
        "unregistrable (retained < 0.01)"
    )
