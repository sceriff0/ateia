"""QC metrics for illumination correction.

Two families:

* **No-reference** metrics that run on the real cluster mosaic (no ground truth):
  seam periodicity (`seam_peak`), boundary discontinuity (`seam_discontinuity`),
  background flatness on a *fixed* ROI (`background_cv`), and — critically — a
  **signal-fidelity** score (`fidelity_score`) that collapses toward 0 when a
  method destroys signal by over-subtraction. The fidelity term is what makes the
  composite un-gameable: a black image has no seams and zero background variance,
  so artifact metrics alone reward zeroing (see research/illumination-correction-
  sota-2026-07-22.md). Full-reference SSIM/RMSE live in `metrics_reference.py`.

* **Resource** capture (`measure`, `_rss_mb`): wall time + absolute peak RSS.
"""
from __future__ import annotations
import sys, time, resource
import numpy as np
from scipy.ndimage import gaussian_filter1d


def _detrend(x, frac=0.05):
    x = x.astype(np.float64)
    sig = max(len(x) * frac, 3.0)
    return x - gaussian_filter1d(x, sig)


# --------------------------------------------------------------------------- #
# Seam / tile-periodicity artifacts
# --------------------------------------------------------------------------- #
def seam_peak(channel, grid, axis) -> float:
    """Fraction of marginal-profile power in a narrow band at the tile pitch.

    Standard periodicity readout (Chang et al., GigaScience 2020): a correctly
    corrected image shows no peak at the grid fundamental. Normalized to
    broadband power so it is amplitude-invariant. Lower is better.
    """
    prof = np.median(channel, axis=1 - axis).astype(np.float64)
    period = grid["pitch_x"] if axis == 1 else grid["pitch_y"]
    x = _detrend(prof)
    x = (x - x.mean()) * np.hanning(len(x))
    power = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(prof))
    kf = int(np.argmin(np.abs(freqs - 1.0 / period)))
    band = power[max(kf - 2, 0):kf + 3].sum()
    return float(band / (power.sum() + 1e-12))


def seam_discontinuity(channel, grid, axis) -> float:
    """Intensity discontinuity at tile boundaries, normalized (BaSiC Γ′-style).

    The most-trusted, hard-to-game seam metric (Peng et al. 2017): the mean
    absolute first-difference *at the tile seams* relative to the mean absolute
    first-difference everywhere. ~1.0 means seams are no sharper than generic
    image structure (good); >1 means a residual step at every tile edge. Uses
    the recovered pitch/phase to locate seams. Lower is better.
    """
    from illum.grid import tile_origins  # local import: avoid cycle at module load
    prof = np.median(channel, axis=1 - axis).astype(np.float64)
    pitch = grid["pitch_x"] if axis == 1 else grid["pitch_y"]
    phase = grid["phase_x"] if axis == 1 else grid["phase_y"]
    d = np.abs(np.diff(prof))
    if d.size == 0:
        return 0.0
    base = d.mean()
    if base <= 1e-12:                      # flat/zeroed profile: no discontinuity
        return 0.0
    seams = [o for o in tile_origins(phase, pitch, 1, len(prof)) if 0 < o < len(d)]
    if not seams:
        return 1.0
    seam_step = np.mean([d[max(o - 1, 0):o + 1].max() for o in seams])
    return float(seam_step / base)


# --------------------------------------------------------------------------- #
# Background flatness (fixed ROI — not a percentile of the corrected image)
# --------------------------------------------------------------------------- #
def background_mask(reference, pct=40.0) -> np.ndarray:
    """Boolean mask of background pixels, defined ONCE on the uncorrected image.

    Thresholding the *corrected* image is circular — the threshold moves with the
    correction being scored, and a method that zeroes the image trivially wins
    (Singh 2014; Model 2001). Deriving the ROI from the reference (uncorrected)
    image keeps the background region fixed across all variants.
    """
    thr = np.percentile(reference, pct)
    return reference <= thr


def background_cv(channel, pct=40.0, mask=None) -> float:
    """Coefficient of variation (std/mean) of the background region — DIAGNOSTIC.

    `mask` (from `background_mask` on the uncorrected image) fixes the ROI; when
    omitted, falls back to a percentile of `channel` itself (legacy behavior). ⚠️
    CV is offset-SENSITIVE: removing a dark pedestal lowers the mean and RAISES CV
    even when absolute flatness improves, so a correct correction can look worse.
    Reported for continuity but NOT used to rank — `background_flatness` is, and
    the composite additionally gates any flatness term with `fidelity_score`.
    """
    bg = channel[mask] if mask is not None else channel[channel <= np.percentile(channel, pct)]
    bg = bg.astype(np.float64)
    if bg.size == 0:
        return 0.0
    return float(bg.std() / (bg.mean() + 1e-12))


def background_flatness(channel, mask) -> float:
    """Absolute std of the fixed background ROI — the offset-invariant flatness.

    Unlike CV, subtracting a constant pedestal leaves std unchanged, so this does
    not penalize legitimate dark/background removal; it drops only when the
    (vignette-induced) spatial variation of the background is actually reduced.
    Lower is better. A zeroed image drives this to 0 (perfect flatness) — that
    degenerate win is neutralized by the fidelity gate in the composite.
    """
    bg = channel[mask].astype(np.float64)
    return float(bg.std()) if bg.size else 0.0


def cross_tile_uniformity(channel, grid) -> float:
    """CV of per-tile mean intensities — a downstream-relevant flatness proxy.

    Shading makes some tiles systematically brighter; correction should equalize
    tile means (KASK 2016: this is what makes a global threshold valid and
    stabilizes cell counts). Reported as a diagnostic, not a ranking term (it is
    itself zeroing-gameable, hence gated by fidelity when used). Lower is better.
    """
    from illum.grid import tile_origins
    py, px = int(round(grid["pitch_y"])), int(round(grid["pitch_x"]))
    ys = tile_origins(grid["phase_y"], grid["pitch_y"], py, channel.shape[0])
    xs = tile_origins(grid["phase_x"], grid["pitch_x"], px, channel.shape[1])
    means = [float(channel[y:y + py, x:x + px].mean()) for y in ys for x in xs]
    if len(means) < 2:
        return 0.0
    means = np.asarray(means, np.float64)
    return float(means.std() / (means.mean() + 1e-12))


# --------------------------------------------------------------------------- #
# Signal fidelity (no-reference) — the anti-gaming gate
# --------------------------------------------------------------------------- #
def foreground_mask(reference, pct=90.0) -> np.ndarray:
    """Boolean mask of the brightest (signal-bearing) pixels of the reference."""
    thr = np.percentile(reference, pct)
    return reference >= thr


def _robust_spread(values) -> float:
    """p99 - p50: a clip-robust measure of signal dynamic range."""
    if values.size == 0:
        return 0.0
    return float(np.percentile(values, 99) - np.percentile(values, 50))


def retained_dynamic_range(before, after, fg_mask) -> float:
    """Fraction of foreground dynamic range retained, in [0, 1].

    Over-subtraction that clips the foreground toward 0 sends this to 0; a
    faithful correction keeps it near 1. Capped at 1 (a correction gets no credit
    for inflating range).
    """
    b = _robust_spread(before[fg_mask].astype(np.float64))
    a = _robust_spread(after[fg_mask].astype(np.float64))
    if b <= 1e-12:
        return 1.0                          # no dynamic range to lose
    return float(np.clip(a / b, 0.0, 1.0))


def foreground_correlation(before, after, fg_mask) -> float:
    """Pearson correlation of before vs after within the foreground, in [0, 1].

    A good correction is monotone in the signal, so relative structure (which
    cells are bright) is preserved → correlation ~1. Zeroing/flattening the
    foreground destroys the correlation → 0. Negative correlation clipped to 0.
    """
    b = before[fg_mask].astype(np.float64).ravel()
    a = after[fg_mask].astype(np.float64).ravel()
    if b.size < 2 or a.std() < 1e-9 or b.std() < 1e-9:
        return 0.0
    r = float(np.corrcoef(b, a)[0, 1])
    return 0.0 if np.isnan(r) else float(np.clip(r, 0.0, 1.0))


def fidelity_score(before, after, fg_mask) -> float:
    """No-reference signal-preservation score in [0, 1] (geometric-mean gate).

    Geometric mean of retained dynamic range and foreground correlation: if
    EITHER collapses (a destroyed image), the score → 0, so it acts as a hard
    multiplicative gate on the composite. Bounded [0, 1].
    """
    dr = retained_dynamic_range(before, after, fg_mask)
    cc = foreground_correlation(before, after, fg_mask)
    return float(np.sqrt(max(dr, 0.0) * max(cc, 0.0)))


# --------------------------------------------------------------------------- #
# Resource capture
# --------------------------------------------------------------------------- #
def _rss_mb():
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def measure(fn, *args, **kw):
    """Run `fn`, returning (result, {seconds, peak_rss_mb}).

    peak_rss_mb is the process's ABSOLUTE high-water mark (ru_maxrss), not a delta
    against a pre-call baseline: ru_maxrss is monotonic, so a delta taken after a
    memory-heavy load (which already set the peak) is ~0 for every call. Absolute
    peak is meaningful in the per-variant SLURM mode where each variant is its own
    process.
    """
    t0 = time.perf_counter()
    result = fn(*args, **kw)
    stats = {"seconds": time.perf_counter() - t0, "peak_rss_mb": _rss_mb()}
    return result, stats
