import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from illum.metrics import (seam_peak, seam_discontinuity, background_cv, background_flatness,
                           background_mask, foreground_mask, retained_dynamic_range,
                           foreground_correlation, fidelity_score, cross_tile_uniformity, measure)
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_seam_peak_drops_after_correction():
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    assert seam_peak(corr, g, axis=1) < seam_peak(ch, g, axis=1)


def test_measure_returns_time_and_memory():
    out, stats = measure(lambda a: a * 2, np.ones(1000))
    assert "seconds" in stats and stats["seconds"] >= 0
    assert "peak_rss_mb" in stats
    assert stats["peak_rss_mb"] > 0        # absolute peak, not a ~0 delta


def test_background_cv_uses_fixed_mask_not_corrected_image():
    d = make_synthetic_mosaic(dark=200.0, vignette_strength=0.4, n_channels=1)
    ch = d["mosaic"][0]
    mask = background_mask(ch)              # ROI fixed on the uncorrected image
    # same mask applied to a zeroed 'correction' -> flat -> CV collapses to ~0
    assert background_cv(np.zeros_like(ch), mask=mask) < 1e-6
    assert mask.sum() > 0


def test_background_flatness_is_offset_invariant():
    # The whole reason it replaces CV: subtracting a constant pedestal must NOT
    # change flatness (CV would rise as the mean drops). Reducing spatial spread
    # (a real correction) must lower it.
    d = make_synthetic_mosaic(dark=200.0, vignette_strength=0.4, n_channels=1)
    ch = d["mosaic"][0]
    mask = background_mask(ch)
    f0 = background_flatness(ch, mask)
    shifted = np.clip(ch.astype(np.float64) - 100.0, 0, None)
    assert abs(background_flatness(shifted, mask) - f0) < 1e-6      # offset-invariant
    assert background_flatness(np.zeros_like(ch), mask) < f0        # flatter -> lower
    # CV, by contrast, is NOT offset-invariant (rises when the pedestal is removed)
    assert background_cv(shifted, mask=mask) > background_cv(ch, mask=mask)


def test_fidelity_is_high_for_faithful_and_zero_for_destroyed():
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    fg = foreground_mask(ch)
    corr = apply_field(ch, estimate_flatfield(ch, g), g)
    assert fidelity_score(ch, corr, fg) > 0.5          # structure preserved
    assert fidelity_score(ch, np.zeros_like(ch), fg) < 0.05   # signal destroyed


def test_retention_and_correlation_bounds():
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    fg = foreground_mask(ch)
    assert retained_dynamic_range(ch, ch, fg) == 1.0
    assert retained_dynamic_range(ch, np.zeros_like(ch), fg) == 0.0
    assert 0.99 <= foreground_correlation(ch, ch, fg) <= 1.0
    assert foreground_correlation(ch, np.zeros_like(ch), fg) == 0.0


def test_seam_discontinuity_flat_image_is_zero():
    d = make_synthetic_mosaic(n_channels=1)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    assert seam_discontinuity(np.zeros_like(d["mosaic"][0]), g, axis=1) == 0.0


def test_cross_tile_uniformity_nonnegative():
    d = make_synthetic_mosaic(vignette_strength=0.5, n_channels=1)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    assert cross_tile_uniformity(d["mosaic"][0], g) >= 0.0
