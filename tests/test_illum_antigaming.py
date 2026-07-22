"""Regression proof that the composite score cannot be gamed by over-subtraction.

The bug this guards against: the old composite (seam_gain + cv_gain) ranked a
variant that drove the image toward black at the TOP, because a black image has no
seams and zero background variance. The fix multiplies the artifact score by a
no-reference signal-fidelity term that collapses for a destroyed image. These
tests assert (a) the NEW composite ranks a faithful correction above a zeroing one,
and (b) the OLD composite would have done the opposite — so the fix is real, not
cosmetic. See research/illumination-correction-sota-2026-07-22.md.
"""
import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.pipeline import Variant, run_variant
from illum.report import rank_variants
from illum.metrics import (seam_peak, background_cv, background_flatness, background_mask,
                           foreground_mask, fidelity_score)
from illum.metrics_reference import ssim
from generate_synthetic_mosaic import make_synthetic_mosaic


def _metrics(stack_grid, before, after):
    """Assemble a single-channel metrics dict the way run_variant does."""
    g = stack_grid
    bg, fg = background_mask(before), foreground_mask(before)
    return {
        "seam_x_before": seam_peak(before, g, 1), "seam_x_after": seam_peak(after, g, 1),
        "seam_y_before": seam_peak(before, g, 0), "seam_y_after": seam_peak(after, g, 0),
        "cv_before": background_cv(before, mask=bg), "cv_after": background_cv(after, mask=bg),
        "flat_before": background_flatness(before, bg), "flat_after": background_flatness(after, bg),
        "fidelity": fidelity_score(before, after, fg),
        "seconds": 1.0, "peak_rss_mb": 100.0,
    }


def _old_composite(m):
    """The pre-fix composite: seam_gain + cv_gain (no fidelity)."""
    sb = (m["seam_x_before"] + m["seam_y_before"]) / 2
    sa = (m["seam_x_after"] + m["seam_y_after"]) / 2
    seam_gain = 1.0 - sa / sb if sb > 0 else 0.0
    cv_gain = 1.0 - m["cv_after"] / m["cv_before"] if m["cv_before"] > 0 else 0.0
    return seam_gain + cv_gain


def _setup():
    # structured=True: dim smooth background + sparse bright cells, like real IF,
    # so background-flatness is a meaningful quantity (not white per-tile noise).
    d = make_synthetic_mosaic(dark=200.0, vignette_strength=0.5, n_channels=1,
                              structured=True)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    corr = run_variant(d["mosaic"], g, Variant("faithful", flatfield="periodic",
                                               dark="const"))["corrected"][0]
    zeroed = np.zeros_like(ch)
    return g, ch, corr, zeroed


def test_new_composite_ranks_faithful_above_zeroing():
    g, ch, corr, zeroed = _setup()
    faithful = {"name": "faithful", "metrics": _metrics(g, ch, corr)}
    zeroing = {"name": "zeroing", "metrics": _metrics(g, ch, zeroed)}
    ranked = rank_variants([zeroing, faithful])       # order-independent
    assert ranked[0]["name"] == "faithful"
    # the zeroing variant is gated to ~0 by its collapsed fidelity
    z = next(r for r in ranked if r["name"] == "zeroing")
    assert z["fidelity"] < 0.05
    assert z["composite"] <= ranked[0]["composite"]


def test_old_composite_was_gameable():
    """Proves the fix mattered: under seam_gain+cv_gain the zeroing variant wins."""
    g, ch, corr, zeroed = _setup()
    faithful_m = _metrics(g, ch, corr)
    zeroing_m = _metrics(g, ch, zeroed)
    # zeroing achieves ~perfect seam + cv gains -> old composite ranks it at/above
    assert _old_composite(zeroing_m) >= _old_composite(faithful_m)


def test_no_reference_composite_agrees_with_ground_truth_ssim():
    """The no-reference fidelity ranking should agree with full-reference SSIM.

    On the synthetic phantom we know the clean signal (content*ff+dark with the
    injected field removed is 'content'); SSIM(corrected, before) is a proxy that a
    faithful correction preserves structure while a zeroed one does not — the same
    ordering the no-reference composite produces.
    """
    g, ch, corr, zeroed = _setup()
    x = ch.astype(np.float64)
    assert ssim(x, corr.astype(np.float64)) > ssim(x, zeroed.astype(np.float64))
    assert fidelity_score(ch, corr, foreground_mask(ch)) > \
           fidelity_score(ch, zeroed, foreground_mask(ch))
