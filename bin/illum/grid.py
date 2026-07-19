"""Recover the periodic tile grid (pitch + phase) from a stitched mosaic.

The vignette repeats at the FOV pitch, so 1-D marginal profiles are periodic.
Pitch is recovered by autocorrelation (1-px lag resolution) with sub-pixel
parabolic refinement; phase by folding the profile on the period and taking
the seam (vignette minimum).
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import gaussian_filter1d, uniform_filter1d


def _detrend(x: np.ndarray, frac: float = 0.05) -> np.ndarray:
    x = x.astype(np.float64)
    sig = max(len(x) * frac, 3.0)
    return x - gaussian_filter1d(x, sig)


def period_from_profile(profile, approx=None, lo=32, hi=None) -> float:
    x = _detrend(profile).astype(np.float64)
    x = (x - x.mean()) * np.hanning(len(x))
    n = len(x)
    ac = np.correlate(x, x, mode="full")[n - 1:]
    ac = ac / (ac[0] + 1e-12)

    if hi is None:
        hi = n // 3
    lo = max(int(lo), 2)
    hi = min(int(hi), n - 2)
    if approx is not None:
        lo = max(lo, int(0.7 * approx))
        hi = min(hi, int(1.3 * approx))
    if hi <= lo:
        raise RuntimeError("no candidate period range; pass/adjust approx_tile")

    win = ac[lo:hi + 1]
    peaks = np.where((win[1:-1] > win[:-2]) & (win[1:-1] > win[2:]))[0] + 1
    if peaks.size:
        thr = 0.3 * win[peaks].max()
        good = peaks[win[peaks] >= thr]
        k = int(good.min()) + lo if good.size else int(np.argmax(win)) + lo
    else:
        k = int(np.argmax(win)) + lo

    if 0 < k < len(ac) - 1:
        y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
        dd = (y0 - 2 * y1 + y2)
        delta = 0.5 * (y0 - y2) / dd if dd != 0 else 0.0
        return float(k + delta)
    return float(k)


def phase_from_profile(profile, period) -> int:
    x = _detrend(profile)
    P = int(round(period))
    n = len(x) // P
    if n < 2:
        return 0
    folded = np.mean([x[i * P:(i + 1) * P] for i in range(n)], axis=0)
    return int(np.argmin(gaussian_filter1d(folded, max(P * 0.02, 1.0))))


def _seam_profile(img: np.ndarray, axis: int, approx: float) -> np.ndarray:
    """Amplitude-invariant seam-detection profile along `axis`.

    Adjacent tiles are stitched by *averaging* their overlap, so local pixel
    variance drops by ~2x inside every overlap band relative to the
    single-tile interior. That variance ratio is a periodic signal at the
    tile pitch that -- unlike raw/median intensity -- does not depend on the
    (random, per-tile) tissue-content amplitude, so it survives large
    tile-to-tile brightness swings that would otherwise swamp a naive
    intensity marginal profile.

    Built from squared first differences (a local-variance proxy), each
    normalized by a wide (~3x period) box-smoothed version of itself so the
    per-tile amplitude scale cancels out, leaving only the relative seam
    dip. Mean-reduced across the orthogonal axis (mean, not median: the
    per-pixel ratio is noisy/skewed and mean converges far more reliably
    over the tens of thousands of samples available).
    """
    win = max(int(round(approx * 3)), 3)
    dv = np.diff(img, axis=axis) ** 2
    smooth = uniform_filter1d(dv, size=win, axis=axis)
    norm = dv / np.maximum(smooth, 1e-9)
    return np.mean(norm, axis=1 - axis)


def recover_grid(stack, approx_tile=None, est_downsample=4) -> dict:
    img = stack.sum(axis=0) if stack.ndim == 3 else stack
    img = img.astype(np.float64)

    approx = approx_tile
    if approx is None:
        # Rough, amplitude-sensitive first pass just to seed the seam-profile
        # window/search range; refined below by the seam profile itself.
        col0 = np.median(img, axis=0)
        row0 = np.median(img, axis=1)
        approx = float(np.mean([
            period_from_profile(col0, approx=None),
            period_from_profile(row0, approx=None),
        ]))

    col = _seam_profile(img, axis=1, approx=approx)
    row = _seam_profile(img, axis=0, approx=approx)
    px = period_from_profile(col, approx=approx)
    py = period_from_profile(row, approx=approx)
    ox = phase_from_profile(col, px)
    oy = phase_from_profile(row, py)
    return {"pitch_y": py, "pitch_x": px, "phase_y": oy, "phase_x": ox}


def tile_origins(phase, pitch, tile_size, extent):
    """Integer start indices of complete tiles spaced on the FLOAT pitch.

    Using round(phase + k*pitch) instead of a fixed integer stride prevents
    sub-pixel pitch error from accumulating across a large mosaic.
    """
    origins, k = [], 0
    while True:
        start = int(round(phase + k * pitch))
        if start + tile_size > extent:
            break
        if start >= 0:
            origins.append(start)
        k += 1
    return origins
